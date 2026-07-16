"""Provision, install, connect, and control provider contracts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, NamedTuple, Protocol, cast
from uuid import UUID

from kdive.domain.capture import CaptureMethod
from kdive.domain.operations.jobs import PowerAction
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.ports._common import config_error
from kdive.providers.ports.handles import SystemHandle, TransportHandle
from kdive.serialization import JsonValue

# The handle-scheme decode set (`TransportHandleData.kind`), NOT the agent-facing transport
# token set. Connectors emit: gdbstub (all), ssh (local drgn-live realization), drgn-live
# (fault-inject). Remote drgn-live emits a bare domain name (unschemed) — never decoded here
# (ADR-0085).
TransportHandleKind = Literal["gdbstub", "ssh", "drgn-live"]
_TRANSPORT_KINDS: frozenset[TransportHandleKind] = frozenset(("gdbstub", "ssh", "drgn-live"))

# Agent-facing debug-session transport values accepted by ``debug.start_session`` and provider
# connectors. Keep this separate from ``TransportHandleKind``: local drgn-live is realized as an
# ``ssh://`` handle, while remote drgn-live is an unschemed domain handle.
DebugTransportKind = Literal["gdbstub", "drgn-live"]
DEBUG_TRANSPORT_KINDS: frozenset[DebugTransportKind] = frozenset(("gdbstub", "drgn-live"))

# The introspection modes a provider can serve, mirroring ``DebugTransportKind`` (ADR-0208):
# ``offline-vmcore`` is ``introspect.from_vmcore``, ``live`` is ``introspect.run``, and
# ``live-script`` is ``introspect.script`` (the live arbitrary-drgn-script tier, ADR-0240). Read
# as part of the ProviderRuntime capability descriptor; empty ⇒ the corresponding tool is
# unsupported.
IntrospectionMode = Literal["offline-vmcore", "live", "live-script"]
INTROSPECTION_MODES: frozenset[IntrospectionMode] = frozenset(
    ("offline-vmcore", "live", "live-script")
)


class TransportHandleData(NamedTuple):
    """A decoded transport handle: the transport kind and its loopback endpoint."""

    kind: TransportHandleKind
    host: str
    port: int

    def encode(self) -> str:
        """Serialize to the ``<kind>://host:port`` wire form."""
        return f"{self.kind}://{self.host}:{self.port}"

    @classmethod
    def decode(cls, raw: str) -> TransportHandleData:
        """Parse a serialized ``<kind>://host:port`` handle."""
        scheme, sep, remainder = raw.partition("://")
        if not sep or scheme not in _TRANSPORT_KINDS:
            raise config_error("transport handle has no known transport scheme")
        kind = cast(TransportHandleKind, scheme)
        host, sep, port_text = remainder.rpartition(":")
        if not sep or not host:
            raise config_error("transport handle must be <kind>://host:port")
        try:
            port = int(port_text)
        except ValueError as exc:
            raise config_error("transport handle port must be numeric") from exc
        if port <= 0 or port > 65535:
            raise config_error("transport handle port is outside 1..65535")
        return cls(kind, host, port)


@dataclass(frozen=True, slots=True)
class InstallRequest:
    """Inputs for staging a built kernel into a System for one Run."""

    system_id: UUID
    run_id: UUID
    kernel_ref: str
    cmdline: str
    method: CaptureMethod = CaptureMethod.HOST_DUMP
    initrd_ref: str | None = None
    debuginfo_ref: str | None = None


class Provisioner(Protocol):
    """Provisioning port keyed on the already-minted System id."""

    def provision(
        self,
        system_id: UUID,
        profile: ProvisioningProfile,
        *,
        overlay_customizers: tuple[Callable[[str], None], ...] = (),
        bootstrap_pubkey: str | None = None,
    ) -> str:
        """Create and start a System, returning the provider domain name.

        ``overlay_customizers`` (ADR-0289, #963) run in provider-defined order against a
        freshly-created overlay only — never on a provision retry that reuses an existing one. A
        provider without a local overlay to customize (e.g. a synthetic or remote-volume-backed
        plane) accepts and ignores this argument.

        ``bootstrap_pubkey`` (ADR-0291, #966) is the System's ensured bootstrap **public** key.
        local-libvirt ignores it (its injection is the pre-boot ``overlay_customizers`` path);
        remote-libvirt injects it into the running guest over the guest agent when its SSH-parity
        forward is configured. The two carriers coexist because the injection phases differ
        (overlay file before boot vs. running guest after agent-ready).

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for invalid provider-specific profile
                data, ``MISSING_DEPENDENCY`` for unavailable provider tools or materialization
                seams, ``PROVISIONING_FAILURE`` for domain/rootfs creation failures,
                ``INFRASTRUCTURE_FAILURE`` for provider-control-plane faults, or
                ``TRANSPORT_FAILURE`` when a remote provider's control channel cannot
                connect (the spec's documented mapping for remote planes).
        """
        ...

    def teardown(self, domain_name: str) -> None:
        """Destroy provider state for a domain name.

        Raises:
            CategorizedError: ``INFRASTRUCTURE_FAILURE`` when the provider cannot complete
                or verify teardown, or ``TRANSPORT_FAILURE`` when a remote provider's
                control channel cannot connect.
        """
        ...

    def reprovision(
        self,
        system_id: UUID,
        profile: ProvisioningProfile,
        *,
        overlay_customizers: tuple[Callable[[str], None], ...] = (),
        bootstrap_pubkey: str | None = None,
    ) -> str:
        """Replace a System's provider state, returning the new provider domain name.

        ``overlay_customizers`` (ADR-0289, #963) and ``bootstrap_pubkey`` (ADR-0291, #966) are
        forwarded the same as :meth:`provision`.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for invalid provider-specific profile
                data, ``PROVISIONING_FAILURE`` for replacement-domain creation failures,
                ``INFRASTRUCTURE_FAILURE`` for teardown/control-plane faults, or
                ``TRANSPORT_FAILURE`` when a remote provider's control channel cannot
                connect.
        """
        ...

    def read_resolved_cpu(self, system_id: UUID) -> dict[str, JsonValue] | None:
        """The running domain's live-verified guest CPU baseline, or ``None`` (ADR-0369).

        Best-effort and side-effect-free: read post-provision to record the CPU the System actually
        booted with (``{model, vendor?, arch, baseline_level?}``). Never raises — a fault, an
        unreadable/unexpanded ``<cpu>``, or a provider with no live-verified reading returns
        ``None``. local-libvirt reads the running domain (host-passthrough resolves to the host
        CPU); remote and fault-inject return ``None`` (remote keeps its selection-time snapshot).
        """
        ...


class Installer(Protocol):
    """Install port keyed on System and Run ids."""

    def install(self, request: InstallRequest) -> None:
        """Install a built kernel into a System and confirm guest readiness.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for invalid capture/install inputs,
                ``STALE_HANDLE`` for vanished artifact refs, ``INFRASTRUCTURE_FAILURE`` for
                store IO failures, ``INSTALL_FAILURE`` for provider install faults,
                ``READINESS_FAILURE`` for guest readiness command failures, or
                ``BOOT_TIMEOUT`` when the guest never becomes ready.
        """
        ...


class Booter(Protocol):
    """Boot port: power-cycle the domain and confirm run-readiness."""

    def boot(self, system_id: UUID, *, accel: str | None = None) -> None:
        """Boot a System after installation and confirm run-readiness.

        ``accel`` is the System's persisted accelerator (``"kvm"`` / ``"tcg"`` / ``None``,
        ADR-0339). The local-libvirt booter scales its boot-readiness window by it (ADR-0341):
        KVM is unscaled, while TCG and an unknown/``None`` accelerator get the generous
        (scaled) window so an over-optimistic classification degrades to a slow-but-correct
        boot rather than a spurious timeout. Other providers accept and ignore it. The default
        ``None`` yields the safe (scaled) window for any caller that omits it.

        Raises:
            CategorizedError: ``INSTALL_FAILURE`` for provider boot faults,
                ``READINESS_FAILURE`` for guest readiness command failures, or
                ``BOOT_TIMEOUT`` when the guest never becomes ready.
        """
        ...


class Connector(Protocol):
    """Connect port for opening and closing debug transports."""

    def open_transport(self, system: SystemHandle, kind: DebugTransportKind) -> TransportHandle:
        """Open a debug transport and return an opaque handle.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for an unknown transport kind,
                ``MISSING_DEPENDENCY`` for unavailable provider seams,
                ``TRANSPORT_FAILURE`` for tunnel allocation faults, or
                ``DEBUG_ATTACH_FAILURE`` when the endpoint cannot be attached.
        """
        ...

    def close_transport(self, handle: TransportHandle) -> None:
        """Close a previously opened transport handle.

        Raises:
            CategorizedError: ``CONFIGURATION_ERROR`` for malformed handles,
                ``MISSING_DEPENDENCY`` for unavailable provider seams, or
                ``TRANSPORT_FAILURE`` when teardown of the tunnel fails.
        """
        ...

    def recorded_ssh_endpoint(self, system: SystemHandle) -> tuple[str, int] | None:
        """Return the recorded SSH ``(host, port)`` for ``system``, or ``None`` (ADR-0271).

        ``None`` means the System was not provisioned with an SSH forward, so no agent SSH is
        available. Providers without a local SSH endpoint to disclose return ``None``.

        Raises:
            CategorizedError: ``INFRASTRUCTURE_FAILURE`` on an unexpected provider read error.
        """
        ...


class Controller(Protocol):
    """Control port keyed on provider domain name."""

    def power(self, domain_name: str, action: PowerAction) -> None:
        """Apply a power operation to a provider domain.

        Raises:
            CategorizedError: ``CONTROL_FAILURE`` for absent domains or provider power
                faults, ``CONFIGURATION_ERROR`` for invalid provider connection
                configuration, ``INFRASTRUCTURE_FAILURE`` for provider setup faults, or
                ``TRANSPORT_FAILURE`` when a remote provider's control channel cannot
                connect.
        """
        ...

    def force_crash(self, domain_name: str) -> None:
        """Trigger a guest crash path for vmcore capture.

        Raises:
            CategorizedError: ``CONTROL_FAILURE`` for absent domains or provider crash
                trigger faults, ``CONFIGURATION_ERROR`` for invalid provider connection
                configuration, ``INFRASTRUCTURE_FAILURE`` for provider setup faults, or
                ``TRANSPORT_FAILURE`` when a remote provider's control channel cannot
                connect.
        """
        ...

    def diagnostic_sysrq(self, domain_name: str, trigger: str) -> None:
        """Inject one non-destructive magic-SysRq keystroke into a guest (ADR-0285).

        ``trigger`` is a single magic-SysRq character from the diagnostic allowlist (the tool
        validates it before enqueue). Injection is fire-and-forget: the resulting kernel dump
        is captured from the console by the worker handler, not returned here.

        Raises:
            CategorizedError: ``CONTROL_FAILURE`` for an absent domain or provider injection
                fault, ``CONFIGURATION_ERROR`` for an unsupported trigger or invalid provider
                connection configuration, ``INFRASTRUCTURE_FAILURE`` for provider setup faults,
                or ``TRANSPORT_FAILURE`` when a remote provider's control channel cannot
                connect. A provider that does not support SysRq injection raises
                ``CONTROL_FAILURE``.
        """
        ...
