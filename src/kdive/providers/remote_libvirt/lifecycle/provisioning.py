"""Remote-libvirt Provisioning plane: disk-image base-OS define/start over TLS (ADR-0080).

`RemoteLibvirtProvisioning` realizes the `Provisioner` port against a remote `qemu+tls://`
host: it renders a gdbstub-enabled domain XML carrying the qemu-guest-agent virtio-serial
channel, creates the per-System qcow2 overlay as a storage-pool volume backed by the
operator-staged base image (no shared filesystem, so no worker-side ``qemu-img``), and
defines+starts the domain over the ADR-0077 mutual-TLS transport.

The **domain definition is the gdbstub port registry**: the per-System port is allocated
by enumerating the ports recorded in the defined ``kdive-`` domains' XML and rendered into
``<qemu:commandline>``, so the record is atomic with ``defineXML``, freed by ``undefine``,
and read over the same TLS connection by the Connect plane (ADR-0079/0080). XML rendering,
gdbstub port enumeration, overlay volume lifecycle, and guest-agent readiness polling live in
focused provider-local collaborators; this facade owns remote config, connection scope, and
define/start/teardown orchestration.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Protocol
from uuid import UUID

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import (
    ProvisioningProfile,
    RemoteLibvirtProfile,
    require_concrete_sizing,
)
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, unbound_remote_config
from kdive.providers.remote_libvirt.connection.transport import (
    RemoteLibvirtConnections,
    open_libvirt_protocol,
    remote_libvirt_connections,
)
from kdive.providers.remote_libvirt.guest.agent import GuestDomain
from kdive.providers.remote_libvirt.guest.bootstrap_key import RemoteBootstrapKeyInjector
from kdive.providers.remote_libvirt.lifecycle.gdb import (
    DOMAIN_PREFIX,
    allocate_gdb_port,
    used_gdb_ports,
    used_ssh_ports,
)
from kdive.providers.remote_libvirt.lifecycle.readiness import Monotonic, Sleep, wait_for_agent
from kdive.providers.remote_libvirt.lifecycle.storage import (
    Pool,
    cleanup_overlay_if_created,
    delete_volume,
    ensure_overlay,
    lookup_pool,
)
from kdive.providers.remote_libvirt.lifecycle.xml import (
    KDIVE_METADATA_NS,
    QEMU_NS,
    overlay_volume_name,
    recorded_gdb_port,
    render_domain_xml,
    render_volume_xml,
)
from kdive.providers.remote_libvirt.lifecycle.xml import (
    disk_pool_strict as _disk_pool_strict,
)
from kdive.providers.shared.runtime_paths import domain_name_for
from kdive.security.secrets.secret_registry import SecretRegistry

__all__ = [
    "KDIVE_METADATA_NS",
    "QEMU_NS",
    "RemoteLibvirtProvisioning",
    "allocate_gdb_port",
    "overlay_volume_name",
    "recorded_gdb_port",
    "render_domain_xml",
    "render_volume_xml",
]

_log = logging.getLogger(__name__)

# Bounded start-failure port advance (ADR-0080 §2): a squatted port or a define→start
# race is skipped without message sniffing; an unrelated start fault fails fast after
# this many attempts.
_START_ATTEMPTS = 3
_AGENT_TIMEOUT_S = 180.0
_AGENT_POLL_S = 2.0


class _Domain(Protocol):
    """The domain slice provisioning uses (duck-typed seam)."""

    def name(self) -> str: ...
    def create(self) -> int: ...
    def destroy(self) -> int: ...
    def undefine(self) -> int: ...
    def isActive(self) -> int: ...  # noqa: N802 - binding name
    def XMLDesc(self, flags: int = 0) -> str: ...  # noqa: N802 - binding name


class _ProvisionConn(Protocol):
    """The connection slice provisioning uses (duck-typed seam)."""

    def defineXML(self, xml: str) -> _Domain: ...  # noqa: N802 - binding name
    def lookupByName(self, name: str) -> _Domain: ...  # noqa: N802 - binding name
    def listAllDomains(self, flags: int = 0) -> list[_Domain]: ...  # noqa: N802 - binding name
    def storagePoolLookupByName(self, name: str) -> Pool: ...  # noqa: N802 - binding name
    def close(self) -> None: ...


class _BootstrapInjector(Protocol):
    """The bootstrap-key injection seam provisioning uses (duck-typed for the test fake)."""

    def inject(self, domain: GuestDomain, pubkey: str) -> None: ...


type OpenProvisionConnection = Callable[[str], _ProvisionConn]


def open_libvirt_provision(uri: str) -> _ProvisionConn:
    """The production opener (live-host path; unit tests inject a fake)."""
    return open_libvirt_protocol(uri)


def _ssh_forward(config: RemoteLibvirtConfig) -> tuple[str, int, int] | None:
    """The SSH forward bundle ``(ssh_addr, ssh_port_min, ssh_port_max)``, or ``None`` if inactive.

    Bundling narrows all three fields with one ``is not None`` check downstream (ADR-0291) so the
    port allocator receives concrete ``int`` bounds.
    """
    if (
        config.ssh_addr is not None
        and config.ssh_port_min is not None
        and config.ssh_port_max is not None
    ):
        return config.ssh_addr, config.ssh_port_min, config.ssh_port_max
    return None


class RemoteLibvirtProvisioning:
    """The realized Provisioner port for a remote qemu+tls host (ADR-0080).

    Buildable without operator config (ADR-0076): the remote connection config is
    resolved per op from the ``systems.toml`` ``[[remote_libvirt]]`` instance via
    ``config_factory`` (ADR-0112), never at construction. All slow seams (connection
    opener, clock, sleep) are injected; unit tests never touch a real host.
    """

    def __init__(
        self,
        *,
        secret_registry: SecretRegistry,
        connections: RemoteLibvirtConnections[_ProvisionConn] | None = None,
        config_factory: Callable[[], RemoteLibvirtConfig] = unbound_remote_config,
        sleep: Sleep = time.sleep,
        monotonic: Monotonic = time.monotonic,
        agent_timeout_s: float = _AGENT_TIMEOUT_S,
        agent_poll_s: float = _AGENT_POLL_S,
        bootstrap_injector: _BootstrapInjector | None = None,
    ) -> None:
        self._connections = connections or remote_libvirt_connections(
            secret_registry=secret_registry,
            config_factory=config_factory,
            open_connection=open_libvirt_provision,
        )
        self._sleep = sleep
        self._monotonic = monotonic
        self._agent_timeout_s = agent_timeout_s
        self._agent_poll_s = agent_poll_s
        self._bootstrap_injector = bootstrap_injector or RemoteBootstrapKeyInjector()

    def provision(
        self,
        system_id: UUID,
        profile: ProvisioningProfile,
        *,
        overlay_customizers: tuple[Callable[[str], None], ...] = (),
        bootstrap_pubkey: str | None = None,
    ) -> str:
        """Define and start the System's disk-image domain; wait for its guest agent.

        Idempotent (ADR-0080 §4): a deterministic name+uuid redefines in place on
        retry, ``create()`` treats already-running as the achieved post-state, the
        overlay is created only when absent, and a retry reuses the System's own
        recorded gdbstub port.

        ``overlay_customizers`` (ADR-0289, #963) is accepted for ``Provisioner`` call-site parity
        but ignored: the remote overlay is a storage-pool volume over TLS, not a local qcow2 file
        ``virt-customize`` can touch. Instead, when SSH parity is configured (``ssh_addr`` +
        ``ssh_range``, ADR-0291), a per-System user-mode SSH forward is rendered and, after the
        guest agent connects, ``bootstrap_pubkey`` is injected into the guest's authorized_keys
        over the guest-agent channel. Both are no-ops when SSH parity is inactive.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for a profile without a remote
                section, missing operator config (incl. the gdbstub listen address),
                or an absent pool/base volume; ``PROVISIONING_FAILURE`` for overlay
                creation, define/start, gdbstub-port exhaustion, a bootstrap-key injection
                failure, an agent that never connects, or a domain that exits during boot;
                ``INFRASTRUCTURE_FAILURE`` for other provider control-plane faults;
                ``TRANSPORT_FAILURE`` when the TLS connect fails.
        """
        del overlay_customizers
        section = self._remote_section(profile)
        require_concrete_sizing(profile)
        config = self._connections.config()
        gdb_addr = config.gdb_addr
        if gdb_addr is None:
            raise CategorizedError(
                "the remote-libvirt instance has no gdb_addr; the gdbstub listen address "
                "is the ACL'd security boundary and must be named explicitly in systems.toml "
                "(ADR-0080)",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
        ssh_forward = _ssh_forward(config)
        domain_name = domain_name_for(system_id)
        with self._connection(config) as conn:
            pool = lookup_pool(conn, config.storage_pool)
            overlay = ensure_overlay(pool, section.base_image_volume, system_id)
            try:
                self._define_and_start(
                    conn,
                    system_id,
                    profile,
                    config=config,
                    gdb_addr=gdb_addr,
                    overlay_name=overlay.name,
                    ssh_forward=ssh_forward,
                )
            except CategorizedError:
                cleanup_overlay_if_created(pool, overlay)
                raise
            # Agent-gate failures deliberately leave the domain (and its overlay) in
            # place: the running/exited domain is the diagnosable artifact, and a
            # provision retry converges without tearing it down (ADR-0080 §4).
            wait_for_agent(
                conn,
                domain_name,
                monotonic=self._monotonic,
                sleep=self._sleep,
                timeout_s=self._agent_timeout_s,
                poll_s=self._agent_poll_s,
            )
            # Inject the bootstrap key over the guest agent once the agent answers (ADR-0291):
            # the pre-SSH channel to a remote guest. No-op when SSH parity is inactive or a
            # System predates the key. Idempotent, so a provision retry re-runs it harmlessly.
            if ssh_forward is not None and bootstrap_pubkey is not None:
                self._bootstrap_injector.inject(conn.lookupByName(domain_name), bootstrap_pubkey)
        return domain_name

    def reprovision(
        self,
        system_id: UUID,
        profile: ProvisioningProfile,
        *,
        overlay_customizers: tuple[Callable[[str], None], ...] = (),
        bootstrap_pubkey: str | None = None,
    ) -> str:
        """Wipe the System's domain + overlay and provision the new profile in place.

        Raises:
            CategorizedError: as :meth:`teardown` and :meth:`provision`.
        """
        self.teardown(domain_name_for(system_id))
        return self.provision(
            system_id,
            profile,
            overlay_customizers=overlay_customizers,
            bootstrap_pubkey=bootstrap_pubkey,
        )

    def teardown(self, domain_name: str) -> None:
        """Destroy+undefine the domain and delete its overlay volume; idempotent.

        The overlay's pool is read from the domain XML while the domain exists (the
        record travels with the domain), falling back to the configured pool when it
        is already gone — pool-config drift cannot silently strand the overlay
        (ADR-0080 §4). Absent domain/volume/pool are achieved post-states.

        Raises:
            CategorizedError: ``INFRASTRUCTURE_FAILURE`` on any libvirt error other
                than the achieved post-states; ``CONFIGURATION_ERROR`` for missing
                operator config; ``TRANSPORT_FAILURE`` when the TLS connect fails.
        """
        config = self._connections.config()
        overlay_name = overlay_volume_name(domain_name.removeprefix(DOMAIN_PREFIX))
        with self._connection(config) as conn:
            recorded_pool = self._teardown_domain(conn, domain_name)
            delete_volume(conn, recorded_pool or config.storage_pool, overlay_name)

    def _connection(self, config: RemoteLibvirtConfig):  # noqa: ANN202 - contextmanager passthrough
        return self._connections.connection(config)

    @staticmethod
    def _remote_section(profile: ProvisioningProfile) -> RemoteLibvirtProfile:
        section = profile.provider.remote_libvirt_section
        if section is None:
            raise CategorizedError(
                "provisioning profile has no remote-libvirt provider section",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
        return section

    def _define_and_start(
        self,
        conn: _ProvisionConn,
        system_id: UUID,
        profile: ProvisioningProfile,
        *,
        config: RemoteLibvirtConfig,
        gdb_addr: str,
        overlay_name: str,
        ssh_forward: tuple[str, int, int] | None,
    ) -> None:
        """Define+start with a bounded port advance on start failure (ADR-0080 §2, ADR-0291).

        A start failure undefines the just-defined domain (transactional) and retries with the
        next free candidate port(s) — unconditionally on the failure's cause, since libvirt does
        not surface bind-vs-other distinctly. Both the gdbstub port and (when SSH parity is
        active) the SSH-forward port are allocated per attempt and advanced together on failure,
        so a squatted host socket for either forward is skipped; an unrelated fault fails the same
        way again and the bounded retry stops.
        """
        domain_name = domain_name_for(system_id)
        used_gdb = used_gdb_ports(conn)
        used_ssh = used_ssh_ports(conn) if ssh_forward is not None else {}
        gdb_tried: set[int] = set()
        ssh_tried: set[int] = set()
        last_error: libvirt.libvirtError | None = None
        for _attempt in range(_START_ATTEMPTS):
            gdb_port = allocate_gdb_port(
                used_gdb,
                own_name=domain_name,
                # Reserve gdb_port_min as the ACL-probe port; Systems start one above it so the
                # gdbstub_acl diagnostic never attaches to a live guest (ADR-0184).
                port_min=config.assignable_gdb_port_min,
                port_max=config.gdb_port_max,
                exclude=gdb_tried,
            )
            ssh_port = self._allocate_ssh_port(used_ssh, domain_name, ssh_forward, ssh_tried)
            xml = self._render(
                system_id,
                profile,
                config,
                gdb_addr=gdb_addr,
                overlay_name=overlay_name,
                gdb_port=gdb_port,
                ssh_forward=ssh_forward,
                ssh_port=ssh_port,
            )
            domain = self._define(conn, xml, system_id)
            try:
                domain.create()
                return
            except libvirt.libvirtError as exc:
                if exc.get_error_code() == libvirt.VIR_ERR_OPERATION_INVALID:
                    return  # already running: the achieved post-state
                self._undefine_quietly(domain)
                gdb_tried.add(gdb_port)
                if ssh_port is not None:
                    ssh_tried.add(ssh_port)
                last_error = exc
        raise CategorizedError(
            f"libvirt failed to start the domain after {_START_ATTEMPTS} attempts",
            category=ErrorCategory.PROVISIONING_FAILURE,
            details={"system_id": str(system_id), "attempts": _START_ATTEMPTS},
        ) from last_error

    @staticmethod
    def _allocate_ssh_port(
        used_ssh: dict[str, int],
        domain_name: str,
        ssh_forward: tuple[str, int, int] | None,
        ssh_tried: set[int],
    ) -> int | None:
        """Allocate this attempt's SSH-forward port, or ``None`` when SSH parity is inactive."""
        if ssh_forward is None:
            return None
        _addr, ssh_min, ssh_max = ssh_forward
        return allocate_gdb_port(
            used_ssh, own_name=domain_name, port_min=ssh_min, port_max=ssh_max, exclude=ssh_tried
        )

    @staticmethod
    def _render(
        system_id: UUID,
        profile: ProvisioningProfile,
        config: RemoteLibvirtConfig,
        *,
        gdb_addr: str,
        overlay_name: str,
        gdb_port: int,
        ssh_forward: tuple[str, int, int] | None,
        ssh_port: int | None,
    ) -> str:
        ssh_addr = ssh_forward[0] if ssh_forward is not None else None
        return render_domain_xml(
            system_id,
            profile,
            pool=config.storage_pool,
            volume=overlay_name,
            gdb_addr=gdb_addr,
            gdb_port=gdb_port,
            network=config.network,
            machine=config.machine,
            ssh_addr=ssh_addr,
            ssh_port=ssh_port,
        )

    @staticmethod
    def _define(conn: _ProvisionConn, xml: str, system_id: UUID) -> _Domain:
        try:
            return conn.defineXML(xml)
        except libvirt.libvirtError as exc:
            raise CategorizedError(
                "libvirt failed to define the domain",
                category=ErrorCategory.PROVISIONING_FAILURE,
                details={"system_id": str(system_id)},
            ) from exc

    @staticmethod
    def _undefine_quietly(domain: _Domain) -> None:
        try:
            domain.undefine()
        except libvirt.libvirtError:
            _log.warning(
                "failed to undefine domain after a failed start; continuing", exc_info=True
            )

    def _teardown_domain(self, conn: _ProvisionConn, domain_name: str) -> str | None:
        """Destroy+undefine; return the pool the domain's disk recorded, if readable.

        "No such domain" on lookup/undefine and "not running" on destroy are achieved
        post-states (the local-libvirt error-code contract, duplicated deliberately).
        """
        try:
            domain = conn.lookupByName(domain_name)
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
                return None
            raise _infra("looking up", domain=domain_name) from exc
        try:
            recorded_pool = _disk_pool_strict(
                domain.XMLDesc(), operation="teardown", domain=domain_name
            )
        except libvirt.libvirtError:
            recorded_pool = None
        try:
            domain.destroy()
        except libvirt.libvirtError as exc:
            if exc.get_error_code() != libvirt.VIR_ERR_OPERATION_INVALID:
                raise _infra("destroying", domain=domain_name) from exc
        try:
            domain.undefine()
        except libvirt.libvirtError as exc:
            if exc.get_error_code() != libvirt.VIR_ERR_NO_DOMAIN:
                raise _infra("undefining", domain=domain_name) from exc
        return recorded_pool


def _infra(verb: str, **details: str) -> CategorizedError:
    return CategorizedError(
        f"libvirt error {verb}",
        category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        details=dict(details),
    )
