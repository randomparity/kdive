"""Ephemeral remote-libvirt build VM: provision a throwaway builder, exec, tear down (ADR-0100).

`EphemeralBuildVm.session` provisions a ``kdive-build-<run_id>`` domain on the configured
remote-libvirt host (a qcow2 overlay over the operator-staged base build image, the
guest-agent channel, generous vCPU/RAM, and **no gdbstub** — a builder is not a debug target),
waits for its guest agent, yields a :class:`GuestExecBuildTransport` bound to the domain, and
tears the domain + overlay down in a ``finally``. The reconciler reaps a leaked builder by
domain marker + owning-BUILD-job liveness (see the ``build_vm_reaper`` module).

The build domain name (``kdive-build-<run_id>``) and overlay name (``kdive-build-<run_id>.qcow2``)
are disjoint from the per-System schemes, and the domain records no gdbstub port, so it is
inert for System gdbstub-port enumeration (ADR-0100). The blocking libvirt calls run only
under the ``live_vm`` gate; orchestration is unit-tested with an injected fake connection.
"""

from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.build import GitSourceRef
from kdive.providers.ports.build_transport import CommandResult
from kdive.providers.remote_libvirt.config import RemoteLibvirtConfig, remote_config_from_inventory
from kdive.providers.remote_libvirt.guest.agent import AgentCommand, qemu_agent_command
from kdive.providers.remote_libvirt.guest.build_transport import GuestExecBuildTransport
from kdive.providers.remote_libvirt.lifecycle.provisioning import (
    open_libvirt_provision,
)
from kdive.providers.remote_libvirt.lifecycle.readiness import (
    Monotonic,
    Sleep,
    wait_for_agent,
    wait_for_network,
)
from kdive.providers.remote_libvirt.lifecycle.storage import (
    delete_volume,
    ensure_named_overlay,
    lookup_pool,
)
from kdive.providers.remote_libvirt.transport import (
    RemoteLibvirtConnections,
    remote_libvirt_connections,
)
from kdive.providers.shared.build_host.workspaces.workspace import redacted_tail
from kdive.providers.shared.libvirt_xml import KDIVE_METADATA_NS, register_kdive_namespace
from kdive.security.secrets.redaction import redact_url_credentials
from kdive.security.secrets.secret_registry import SecretRegistry

__all__ = [
    "BUILD_DOMAIN_PREFIX",
    "BuildVmTiming",
    "EphemeralBuildVm",
    "build_domain_name",
    "build_overlay_volume_name",
    "ephemeral_build_session",
    "render_build_domain_xml",
]

_log = logging.getLogger(__name__)

BUILD_DOMAIN_PREFIX = "kdive-build-"
_GUEST_AGENT_CHANNEL = "org.qemu.guest_agent.0"

# Fixed build-VM sizing: a kernel compile wants several cores and headroom. Tunable via a
# follow-up if an operator's host topology needs it (no speculative env knob today).
_BUILD_VCPUS = 4
_BUILD_MEMORY_MIB = 8192
_BUILD_ARCH = "x86_64"

_AGENT_TIMEOUT_S = 180.0
_AGENT_POLL_S = 2.0

# A default route is installed exactly when the guest's DHCP lease lands, so its presence is the
# precise "network is up" signal. /proc/net/route is kernel truth; cut+grep avoid an iproute2 dep.
_DEFAULT_ROUTE_PROBE = "cut -f2 /proc/net/route | grep -qx 00000000"
_NETWORK_PROBE_ARGV = ["/bin/sh", "-c", _DEFAULT_ROUTE_PROBE]
_NETWORK_PROBE_CALL_TIMEOUT_S = 10
_NETWORK_TIMEOUT_S = 120.0
_NETWORK_POLL_S = 2.0

# A default route is necessary but not sufficient for egress to a specific source (ADR-0155): DNS
# may be broken, the guest-subnet->internet hop may be policy-dropped while the route still exists,
# or the remote may be unreachable from the guest's vantage. After the route gate, one bounded
# in-guest `git ls-remote` to the configured source confirms the egress the clone needs — using
# the remote's own protocol (https/ssh/git), so it cannot drift from how the clone dials it. It
# probes HEAD, NOT the configured ref: the clone resolves an arbitrary ref/sha (a bare commit is
# not an advertised ref, so `ls-remote <remote> <sha>` would fail a reachable host); ref existence
# stays the clone's contract.
_EGRESS_PROBE_CALL_TIMEOUT_S = 30


@dataclass(frozen=True)
class BuildVmTiming:
    """Clock and timeout seams for build-VM guest-agent + network readiness."""

    sleep: Sleep = time.sleep
    monotonic: Monotonic = time.monotonic
    agent_timeout_s: float = _AGENT_TIMEOUT_S
    agent_poll_s: float = _AGENT_POLL_S
    network_timeout_s: float = _NETWORK_TIMEOUT_S
    network_poll_s: float = _NETWORK_POLL_S
    egress_probe_timeout_s: int = _EGRESS_PROBE_CALL_TIMEOUT_S


_DEFAULT_BUILD_VM_TIMING = BuildVmTiming()


def build_domain_name(run_id: UUID) -> str:
    """The ephemeral build VM's domain name (the reaper marker), disjoint from System names."""
    return f"{BUILD_DOMAIN_PREFIX}{run_id}"


def build_overlay_volume_name(run_id: UUID) -> str:
    """The build VM's overlay volume name, disjoint from the per-System overlay scheme."""
    return f"{BUILD_DOMAIN_PREFIX}{run_id}.qcow2"


def render_build_domain_xml(
    run_id: UUID,
    *,
    pool: str,
    volume: str,
    network: str,
    machine: str,
    vcpus: int = _BUILD_VCPUS,
    memory_mib: int = _BUILD_MEMORY_MIB,
    arch: str = _BUILD_ARCH,
) -> str:
    """Render the build VM's domain XML: agent channel + overlay disk + network, no gdbstub.

    Unlike the System domain (ADR-0080), this records no ``<qemu:commandline>`` gdbstub args —
    a builder is not a debug target — so it is inert for ``used_gdb_ports`` enumeration.
    """
    register_kdive_namespace()
    domain = ET.Element("domain", type="kvm")
    ET.SubElement(domain, "name").text = build_domain_name(run_id)
    ET.SubElement(domain, "uuid").text = str(run_id)
    ET.SubElement(domain, "memory", unit="MiB").text = str(memory_mib)
    ET.SubElement(domain, "vcpu").text = str(vcpus)
    os_el = ET.SubElement(domain, "os")
    ET.SubElement(os_el, "type", arch=arch, machine=machine).text = "hvm"
    ET.SubElement(os_el, "boot", dev="hd")
    features = ET.SubElement(domain, "features")
    ET.SubElement(features, "acpi")
    devices = ET.SubElement(domain, "devices")
    disk = ET.SubElement(devices, "disk", type="volume", device="disk")
    ET.SubElement(disk, "driver", name="qemu", type="qcow2")
    ET.SubElement(disk, "source", pool=pool, volume=volume)
    ET.SubElement(disk, "target", dev="vda", bus="virtio")
    interface = ET.SubElement(devices, "interface", type="network")
    ET.SubElement(interface, "source", network=network)
    ET.SubElement(interface, "model", type="virtio")
    channel = ET.SubElement(devices, "channel", type="unix")
    ET.SubElement(channel, "target", type="virtio", name=_GUEST_AGENT_CHANNEL)
    metadata = ET.SubElement(domain, "metadata")
    ET.SubElement(metadata, f"{{{KDIVE_METADATA_NS}}}build").text = str(run_id)
    return ET.tostring(domain, encoding="unicode")


class _Domain(Protocol):
    def name(self) -> str: ...
    def create(self) -> int: ...
    def destroy(self) -> int: ...
    def undefine(self) -> int: ...


class _BuildConn(Protocol):
    def defineXML(self, xml: str) -> _Domain: ...  # noqa: N802 - binding name
    def lookupByName(self, name: str) -> Any: ...  # noqa: N802 - binding name
    def storagePoolLookupByName(self, name: str) -> Any: ...  # noqa: N802 - binding name
    def close(self) -> None: ...


class EphemeralBuildVm:
    """Provision/teardown a throwaway remote-libvirt build VM (ADR-0100).

    Buildable without operator config (ADR-0076): the remote connection config is resolved per op
    from the ``systems.toml`` ``[[remote_libvirt]]`` instance via ``config_factory`` (ADR-0112).
    All slow seams (connection opener, agent command, clock, sleep) are injected; unit tests never
    touch a real host.
    """

    def __init__(
        self,
        *,
        secret_registry: SecretRegistry,
        connections: RemoteLibvirtConnections[_BuildConn] | None = None,
        agent_command: AgentCommand = qemu_agent_command,
        timing: BuildVmTiming = _DEFAULT_BUILD_VM_TIMING,
    ) -> None:
        self._secret_registry = secret_registry
        self._connections = connections or remote_libvirt_connections(
            secret_registry=secret_registry,
            config_factory=remote_config_from_inventory,
            open_connection=open_libvirt_provision,
        )
        self._agent_command = agent_command
        self._timing = timing

    @contextmanager
    def session(
        self,
        base_image_volume: str,
        *,
        run_id: UUID,
        source: GitSourceRef | None = None,
        wait_network: bool = True,
    ) -> Iterator[GuestExecBuildTransport]:
        """Provision the build VM, yield a transport bound to it, tear it down on exit.

        Args:
            base_image_volume: The operator-staged base build-image volume to overlay.
            run_id: The owning Run; names the domain/overlay and is the reaper marker.
            source: The configured git build source. When supplied, the session runs a bounded
                in-guest egress preflight (`git ls-remote`) to it after the route gate and before
                yielding, so an unreachable source fails the gate naming the source rather than the
                clone (ADR-0155). ``None`` (a warm-tree source, or a caller that supplies none)
                keeps the route-only behavior.
            wait_network: When ``True`` (the BUILD default), block until the guest has a default
                route before yielding — the clone needs working network. The
                ``ephemeral_libvirt_buildhost_agent`` diagnostic passes ``False`` (ADR-0167): it
                asserts only guest-agent reachability, so it must not wait for — or fail on — the
                network, and the trivial command it runs needs none.

        Yields:
            A :class:`GuestExecBuildTransport` bound to the live build VM's domain.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for missing operator config / absent
                pool/base volume / a source the build VM cannot reach; ``PROVISIONING_FAILURE``
                for overlay/define/start, an agent that never connects, or a guest network that
                never comes up; ``TRANSPORT_FAILURE`` when the TLS connect fails or the agent
                drops mid-probe.
        """
        config = self._connections.config()
        domain_name = build_domain_name(run_id)
        with self._connection(config) as conn:
            pool = lookup_pool(conn, config.storage_pool)
            ensure_named_overlay(pool, base_image_volume, build_overlay_volume_name(run_id))
            try:
                self._define_and_start(conn, run_id, config=config)
                wait_for_agent(
                    conn,
                    domain_name,
                    monotonic=self._timing.monotonic,
                    sleep=self._timing.sleep,
                    timeout_s=self._timing.agent_timeout_s,
                    poll_s=self._timing.agent_poll_s,
                )
                transport = GuestExecBuildTransport(
                    domain=conn.lookupByName(domain_name),
                    agent_command=self._agent_command,
                    secret_registry=self._secret_registry,
                )
                if wait_network:
                    self._wait_for_network(transport, domain_name)
                if source is not None:
                    self._preflight_egress(transport, source)
                yield transport
            finally:
                self._teardown(conn, run_id, config)

    def _preflight_egress(self, transport: GuestExecBuildTransport, source: GitSourceRef) -> None:
        """Confirm the build guest can reach the configured source before the clone (ADR-0155).

        Runs one bounded in-guest ``git ls-remote --quiet --exit-code -- <remote> HEAD`` over the
        guest agent. ``rc 0`` means the guest resolved DNS, completed the handshake, and reached
        the repo — the egress the clone needs. A non-zero rc raises ``CONFIGURATION_ERROR`` naming
        the redacted remote with the git stderr surfaced (``git ls-remote`` returns 128 for both
        unreachable-host and repo-not-found, indistinguishable from the exit code, so the stderr
        carries the specific cause). A raised ``CategorizedError`` (agent dropped) propagates
        unchanged — ``wait_for_agent`` already confirmed the channel.

        The ``--`` end-of-options separator forces the remote to be parsed as the repository
        operand, so a remote that starts with ``-`` cannot be smuggled in as a git option — this
        preflight runs before the clone's own leading-dash guard (``_validate_git_arg``).
        """
        result = transport.run(
            ["git", "ls-remote", "--quiet", "--exit-code", "--", source.remote, "HEAD"],
            cwd="/",
            timeout_s=self._timing.egress_probe_timeout_s,
        )
        if result.returncode != 0:
            raise CategorizedError(
                f"build VM cannot reach build source {redact_url_credentials(source.remote)}",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={
                    "remote": redact_url_credentials(source.remote),
                    "stderr": redacted_tail(result.stderr, self._secret_registry),
                },
            )

    def _wait_for_network(self, transport: GuestExecBuildTransport, domain_name: str) -> None:
        """Block until the build guest has a default route, so the clone sees working network.

        A non-zero probe rc means "no route yet, keep polling"; a raised CategorizedError (the
        agent dropped) propagates. On the deadline, the last probe output is surfaced so a broken
        probe (missing cut/grep) is diagnosable rather than a bare timeout (ADR-0144).
        """
        last: list[CommandResult] = []

        def probe() -> bool:
            result = transport.run(
                _NETWORK_PROBE_ARGV, cwd="/", timeout_s=_NETWORK_PROBE_CALL_TIMEOUT_S
            )
            last.append(result)
            return result.returncode == 0

        def timeout_detail() -> dict[str, object]:
            if not last:
                return {}
            return {
                "probe_stderr": redacted_tail(last[-1].stderr, self._secret_registry),
                "probe_stdout": last[-1].stdout[-200:],
            }

        wait_for_network(
            probe,
            domain_name,
            monotonic=self._timing.monotonic,
            sleep=self._timing.sleep,
            timeout_s=self._timing.network_timeout_s,
            poll_s=self._timing.network_poll_s,
            timeout_detail=timeout_detail,
        )

    def _connection(self, config: RemoteLibvirtConfig) -> Any:
        return self._connections.connection(config)

    def _define_and_start(
        self, conn: _BuildConn, run_id: UUID, *, config: RemoteLibvirtConfig
    ) -> None:
        """Define+start the build domain; an already-running domain is the achieved post-state."""
        xml = render_build_domain_xml(
            run_id,
            pool=config.storage_pool,
            volume=build_overlay_volume_name(run_id),
            network=config.network,
            machine=config.machine,
        )
        try:
            domain = conn.defineXML(xml)
        except libvirt.libvirtError as exc:
            raise CategorizedError(
                "libvirt failed to define the build VM",
                category=ErrorCategory.PROVISIONING_FAILURE,
                details={"run_id": str(run_id)},
            ) from exc
        try:
            domain.create()
        except libvirt.libvirtError as exc:
            if exc.get_error_code() == libvirt.VIR_ERR_OPERATION_INVALID:
                return  # already running: the achieved post-state
            raise CategorizedError(
                "libvirt failed to start the build VM",
                category=ErrorCategory.PROVISIONING_FAILURE,
                details={"run_id": str(run_id)},
            ) from exc

    def _teardown(self, conn: _BuildConn, run_id: UUID, config: RemoteLibvirtConfig) -> None:
        """Destroy+undefine the build domain and delete its overlay; best-effort (reaper backstops).

        Absent domain / not-running / absent volume are achieved post-states. Teardown never
        raises — a failure leaves a leak the reconciler reaps by job liveness.
        """
        domain_name = build_domain_name(run_id)
        try:
            domain = conn.lookupByName(domain_name)
            try:
                domain.destroy()
            except libvirt.libvirtError as exc:
                if exc.get_error_code() != libvirt.VIR_ERR_OPERATION_INVALID:
                    raise
            domain.undefine()
        except libvirt.libvirtError as exc:
            if exc.get_error_code() != libvirt.VIR_ERR_NO_DOMAIN:
                _log.warning(
                    "build VM %s domain teardown failed; reaper reclaims",
                    domain_name,
                    exc_info=True,
                )
        try:
            delete_volume(conn, config.storage_pool, build_overlay_volume_name(run_id))
        except CategorizedError:
            _log.warning(
                "build VM %s overlay delete failed; reaper reclaims",
                domain_name,
                exc_info=True,
            )


@contextmanager
def ephemeral_build_session(
    base_image_volume: str,
    secret_registry: SecretRegistry,
    *,
    run_id: UUID,
    source: GitSourceRef | None = None,
    wait_network: bool = True,
) -> Iterator[GuestExecBuildTransport]:
    """Module-level seam: build a default :class:`EphemeralBuildVm` and run its session.

    The BUILD handler imports this so a test can substitute a fake session without a libvirt
    host; production delegates to a default-seam :class:`EphemeralBuildVm`. ``source`` is the
    configured git build source for the pre-clone egress preflight (ADR-0155); ``None`` keeps the
    route-only readiness behavior. ``wait_network=False`` is the agent-reachability diagnostic's
    seam (ADR-0167): provision + wait-for-agent + yield, without the network gate.
    """
    vm = EphemeralBuildVm(secret_registry=secret_registry)
    with vm.session(
        base_image_volume, run_id=run_id, source=source, wait_network=wait_network
    ) as transport:
        yield transport
